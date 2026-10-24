"""Synthetic originals and real Git; storage integrity is not investment-research quality."""
import base64
from copy import deepcopy
from dataclasses import asdict
import io
import json
import zipfile

import pytest

from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_delivery import (
    ResearchDelivery, START, END, delivery_manifest, stage_key, validate_delivery, verify_fresh,
)
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.store_audit import audit_store
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, Conflict, RecordError, RevisionInput, SourceRef, StoreUnavailable, UnknownWrite, encoded,
)
from decision_kernel.domain.research_artifacts import ArtifactInput, MAX_ARTIFACT_BYTES
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.methods.research import research_method

EXTERNAL_METHOD = "decision-os-research-external-full-v1"


def workbook_bytes():
    """A minimal zipped OOXML workbook with a formula, preserved rather than evaluated."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        parts = {
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
            "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Synthetic" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><v>3</v></c><c r="B1"><f>A1*2</f><v>6</v></c></row></sheetData></worksheet>',
        }
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            z.writestr(info, text.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def delivery_payload(*, method_id=None):
    html = ('<!doctype html>\r\n<style>body{margin:2em}</style><nav>原导航</nav>' + '<p>合成原文与格式</p>' * 3000).encode()
    return {"schema": "research-delivery-v1", "rights_confirmed": True,
        "rights_basis": "Synthetic engineering originals authorized for this test only.",
        "request": {"case_id": "synthetic-delivery", "kind": "full", "mandate": "合成研究委托",
            "questions": [{"question_id": "cash", "text": "利润怎样变成现金"},
                          {"question_id": "value", "text": "估值要求什么"}]},
        "progress": {"body": "合成阶段研究：不是正式投资报告。", "stop": "continue", "stop_reason": "还有分析待做",
            "sources": [], "findings": [
                {"question_id": "cash", "status": "open", "finding": "已取得数据未分析", "evidence": [0],
                 "remaining_kind": "acquired-unanalysed", "next_action": "核现金桥"},
                {"question_id": "value", "status": "reframed", "finding": "估值问题改写，但未完成重算",
                 "next_action": "重算估值"}],
            "challenges": [{"question_id": "cash", "argument": "现金不足可能来自扩张", "disposition": "unresolved",
                            "response": "保留待核", "evidence": [0]}]},
        "revision_id": "r1", "method_sha256": research_method("full", method_id=method_id)["sha256"], "body_mode": "snapshot",
        "items": [
            {"id": "html", "role": "report-original", "status": "include", "required": True, "reason": "",
             "artifact": asdict(ArtifactInput.from_bytes("html-original", "完整原文.HTML", "text/html", html))},
            {"id": "model", "role": "calculation-file", "status": "include", "required": True, "reason": "",
             "artifact": asdict(ArtifactInput.from_bytes("model-original", "计算原件.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", workbook_bytes()))},
            {"id": "filing", "role": "source-evidence", "status": "include", "required": True, "reason": "",
             "source": {"source_id": "synthetic-filing", "kind": "dataset", "title": "合成源数据",
                        "origin": "synthetic:filing", "text": "metric,value\r\nrevenue,3\r\ncash,2\r\n"}}]}


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "records.git"))


def continued(store, payload, receipt):
    result = deepcopy(payload)
    result["request"] = ResearchWorkflow(store).prepare(case_id=payload["request"]["case_id"])["request"]
    result["revision_id"] = "r2"
    result["body_mode"] = "delta"
    result["progress"]["body"] = "合成增量，阅读时需要原完整研究。"
    manifest = delivery_manifest(store.read_revision(BlobRef(**receipt["reference"]))["body"])
    result["items"] = []
    for item in manifest["items"]:
        row = {k: item[k] for k in ("id", "role", "status", "required", "reason")}
        if item["status"] == "retained":
            row["status"] = "include"
            row[item["kind"] + "_ref"] = item["reference"]
        result["items"].append(row)
    return result


@pytest.mark.parametrize("raw", [b"", b"\x00\xff\r\n", b"<script>throw 'inert'</script>", workbook_bytes()])
def test_artifact_exact_bytes_version_and_same_intent_replay(store, raw):
    artifacts = ResearchArtifacts(store)
    item = ArtifactInput.from_bytes("original", "原始文件.bin", "application/octet-stream", raw)
    ref = artifacts.put("artifact-one", item)
    assert artifacts.put("artifact-one", item) == ref
    read = artifacts.read(ref)
    assert base64.b64decode(read["data_base64"], validate=True) == raw
    assert read["executed"] is False and read["bytes_verified"] is True
    new = ArtifactInput.from_bytes("original", "原始文件.bin", "application/octet-stream", raw + b"new")
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        artifacts.put("artifact-one", new)
    newer = artifacts.put("artifact-two", new)
    assert newer.path != ref.path and artifacts.read(ref) == read


@pytest.mark.parametrize("change", [
    {"name": "../secret"}, {"name": "a\\b"}, {"name": "x\nname"}, {"name": ""},
    {"artifact_id": "../bad"}, {"size": True}, {"size": -1}, {"size": MAX_ARTIFACT_BYTES + 1},
    {"sha256": "0" * 64}, {"data_base64": "!!!"}, {"data_base64": "YQ==\n"},
    {"data_base64": "YR=="}, {"media_type": "application/x-executable"},
])
def test_artifact_shape_and_bytes_never_silently_normalized(change):
    item = asdict(ArtifactInput.from_bytes("a", "a.bin", "application/octet-stream", b"a"))
    with pytest.raises(RecordError):
        ArtifactInput.from_dict({**item, **change})


@pytest.mark.parametrize("method_id", [None, EXTERNAL_METHOD])
def test_complete_delivery_originals_source_refs_and_new_reader(store, method_id):
    payload = delivery_payload(method_id=method_id)
    original = deepcopy(payload)
    saved = ResearchDelivery(store).deliver(payload, submission_id="full-one")
    fresh = lambda: VersionedStore(GitStore(store.backend.root))
    result = verify_fresh(saved, fresh)
    assert payload == original and result["readback"] == "fresh-reader-verified"
    assert result["delivery_verification"]["status"] == "verified-declared-inventory"
    assert result["research_assessment"] == "not-performed" and result["human_acceptance"] == "not-confirmed"
    manifest = delivery_manifest(fresh().read_revision(BlobRef(**saved["reference"]))["body"])
    for expected, actual in zip(payload["items"][:2], manifest["items"][:2]):
        view = ResearchArtifacts(fresh()).read(actual["reference"])
        assert view["data_base64"] == expected["artifact"]["data_base64"]
        assert view["metadata"]["name"] == expected["artifact"]["name"]
    record = fresh().read_revision(BlobRef(**saved["reference"]))["record"]["input"]
    method = research_method("full", method_id=method_id)
    assert record["method"] == method["id"] + "@sha256:" + method["sha256"]
    assert ResearchDelivery(fresh()).open(BlobRef(**saved["reference"]))["method"] == method
    assert record["sources"] == [manifest["items"][2]["reference"]]
    assert fresh().read_source(SourceRef.from_dict(record["sources"][0]))["data"][0]["value"] == "3"
    assert ResearchDelivery(store).deliver(payload, submission_id="full-one")["reference"] == saved["reference"]
    assert audit_store(fresh(), store.backend.head())["artifact_versions"] == 3  # two originals and intent


@pytest.mark.parametrize("method_id", [None, EXTERNAL_METHOD])
def test_complete_full_then_delta_restores_original_and_pending_reframed_work(store, method_id):
    payload = delivery_payload(method_id=method_id)
    first = ResearchDelivery(store).deliver(payload, submission_id="one")
    next_payload = continued(store, payload, first)
    second = ResearchDelivery(store).deliver(next_payload, submission_id="two")
    head = store.backend.head()
    opened = ResearchDelivery(store).open(BlobRef(**second["reference"]))
    assert opened["method"] == research_method("full", method_id=method_id)
    assert opened["delivery"]["body_mode"] == "delta"
    assert opened["history"][0]["research"]["reference"] == first["reference"]
    assert payload["progress"]["body"] in opened["history"][0]["research"]["body"]
    assert opened["next_history_reference"] is None and store.backend.head() == head
    plan = opened["continuation"]
    assert {a["question_id"] for a in plan["actions"]} == {"cash", "value"}
    assert plan["actions"][1]["action"] == "review-declared-followup"
    assert plan["retained_findings"][0]["question_id"] == "value"
    assert "NONOPEN_FINDING_HAS_DECLARED_FOLLOWUP" in plan["review_flags"]
    assert opened["request"]["predecessor"] == second["reference"]
    # Old intent still reconstructs its original exact revision after current moves.
    assert ResearchDelivery(store).deliver(payload, submission_id="one")["reference"] == first["reference"]


@pytest.mark.parametrize("before_id,after_id", [
    ("decision-os-research-full-v2", EXTERNAL_METHOD),
    (EXTERNAL_METHOD, "decision-os-research-full-v2"),
])
def test_delivery_method_change_requires_reason_before_intent_or_material_staging(store, before_id, after_id):
    payload = delivery_payload(method_id=before_id)
    first = ResearchDelivery(store).deliver(payload, submission_id="one")
    successor = continued(store, payload, first)
    method = research_method("full", method_id=after_id)
    successor["method_sha256"] = method["sha256"]
    head = store.backend.head()
    with pytest.raises(RecordError, match="EXPLICIT_SCOPE_CHANGE"):
        ResearchDelivery(store).deliver(successor, submission_id="two")
    assert store.backend.head() == head
    assert store.current(payload["request"]["case_id"]) == BlobRef(**first["reference"])
    successor["request"]["scope_change_reason"] = "显式选择另一研究方法，继续保留原问题与未解挑战"
    second = ResearchDelivery(store).deliver(successor, submission_id="two")
    opened = ResearchDelivery(VersionedStore(GitStore(store.backend.root))).open(BlobRef(**second["reference"]))
    assert opened["method"] == method and opened["history"][0]["research"]["reference"] == first["reference"]
    assert opened["continuation"]["unresolved_challenges"][0]["argument"] == payload["progress"]["challenges"][0]["argument"]


@pytest.mark.parametrize("status", ["missing", "unsupported", "rights-excluded"])
def test_missing_file_saves_partial_but_not_complete(store, status):
    payload = delivery_payload()
    item = payload["items"][1]
    item.pop("artifact")
    item.update(status=status, reason="This original is unavailable or cannot be published.")
    saved = ResearchDelivery(store).deliver(payload, submission_id="partial")
    check = ResearchDelivery(store).verify(BlobRef(**saved["reference"]))
    assert check["status"] == "partial" and check["items"][1]["readback"] == "not-retained"
    assert store.current(payload["request"]["case_id"]) == BlobRef(**saved["reference"])


def test_not_applicable_is_accounted_but_not_misrepresented_as_saved_file(store):
    payload = delivery_payload()
    payload["items"][1].pop("artifact")
    payload["items"][1].update(status="not-applicable", required=False, reason="Synthetic mandate has no workbook.")
    saved = ResearchDelivery(store).deliver(payload, submission_id="na")
    check = ResearchDelivery(store).verify(BlobRef(**saved["reference"]))
    assert check["status"] == "verified-declared-inventory"
    assert "reference" not in check["items"][1] and check["items"][1]["status"] == "not-applicable"


@pytest.mark.parametrize("change", [
    lambda p: p.update(rights_confirmed=False),
    lambda p: p.update(rights_basis=""),
    lambda p: p.update(body_mode="delta"),
    lambda p: p["items"].pop(1),
    lambda p: p["items"].append(deepcopy(p["items"][0])),
    lambda p: p["items"][1].update(status="rights-excluded", reason="No rights"),
    lambda p: p["items"][0].update(role="source-evidence"),
    lambda p: p["progress"]["findings"][0].update(evidence=[True]),
    lambda p: p["progress"]["findings"][0].update(evidence=[[0]]),
    lambda p: p["progress"]["findings"][0].update(evidence=[9]),
    lambda p: p["progress"].update(body="injected" + START),
    lambda p: p.update(method_id=EXTERNAL_METHOD),
    lambda p: p.update(method_sha256="0" * 64),
    lambda p: p["request"].update(method_id=EXTERNAL_METHOD),
    lambda p: p["progress"].update(method_id=EXTERNAL_METHOD),
])
def test_invalid_delivery_fails_before_creating_any_store_effect(store, change):
    payload = delivery_payload(); change(payload)
    head = store.backend.head()
    with pytest.raises(RecordError):
        ResearchDelivery(store).deliver(payload, submission_id="bad")
    assert store.backend.head() == head


def test_declared_followup_cannot_be_signed_concluded_through_delivery(store):
    payload = delivery_payload()
    payload["progress"]["findings"][0] = {"question_id": "cash", "status": "answered", "finding": "Done"}
    payload["progress"].update(stop="concluded", challenges=[], challenge_note="Synthetic review")
    with pytest.raises(RecordError, match="DECLARED_FOLLOWUP_PREVENTS_CONCLUSION"):
        ResearchDelivery(store).deliver(payload, submission_id="concluded")


@pytest.mark.parametrize("changed_field", ["body", "method"])
def test_original_intent_reservation_prevents_mixing_changed_partial_payload(store, monkeypatch, changed_field):
    payload = delivery_payload()
    original = store.backend.commit
    commits = []
    def interrupt(files, *, expected):
        result = original(files, expected=expected)
        commits.append(result)
        if len(commits) == 2:
            raise UnknownWrite("SIMULATED_LOST_RESPONSE")
        return result
    monkeypatch.setattr(store.backend, "commit", interrupt)
    with pytest.raises(UnknownWrite):
        ResearchDelivery(store).deliver(payload, submission_id="same-intent")
    monkeypatch.setattr(store.backend, "commit", original)
    current = store.backend.head()
    changed = deepcopy(payload)
    if changed_field == "body":
        changed["progress"]["body"] += " changed"
    else:
        changed["method_sha256"] = research_method("full", method_id=EXTERNAL_METHOD)["sha256"]
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        ResearchDelivery(store).deliver(changed, submission_id="same-intent")
    assert store.backend.head() == current
    saved = ResearchDelivery(store).deliver(payload, submission_id="same-intent")
    assert ResearchDelivery(store).verify(BlobRef(**saved["reference"]))["status"] == "verified-declared-inventory"


def test_old_inventory_cannot_disappear_without_explicit_disposition(store):
    payload = delivery_payload()
    payload["items"].append({**deepcopy(payload["items"][0]), "id": "notes", "role": "supporting-file"})
    first = ResearchDelivery(store).deliver(payload, submission_id="one")
    new = continued(store, payload, first)
    new["items"].pop()
    head = store.backend.head()
    with pytest.raises(RecordError, match="PREVIOUS_DELIVERY_ITEMS_MUST_BE_ACCOUNTED"):
        ResearchDelivery(store).deliver(new, submission_id="two")
    assert store.backend.head() == head


def test_fresh_reader_failure_keeps_known_saved_revision(store):
    receipt = ResearchDelivery(store).deliver(delivery_payload(), submission_id="one")
    def unavailable():
        raise StoreUnavailable("NO_ACCESS")
    checked = verify_fresh(receipt, unavailable)
    assert checked["reference"] == receipt["reference"] and checked["readback"] == "failed"
    assert checked["delivery_verification"]["status"] == "failed"


def test_artifact_corruption_detected_and_older_version_intact(store):
    art = ResearchArtifacts(store)
    ref = art.put("one", ArtifactInput.from_bytes("doc", "doc.bin", "application/octet-stream", b"abc"))
    path = ref.path.rsplit("/", 1)[0] + "/content.bin"
    new = store.backend.commit({path: b"bad"}, expected=store.backend.head())
    assert base64.b64decode(art.read(ref)["data_base64"]) == b"abc"
    with pytest.raises(RecordError):
        art.read(store.backend.reference(new, ref.path))
    with pytest.raises(RecordError):
        audit_store(store, new)


def test_legacy_narrative_is_readable_without_invented_inventory(store):
    ref = store.submit_revision("legacy", RevisionInput("legacy", "one", "full", "Old text",
                               "Synthetic", "old-method", unresolved=("No saved source",)))
    opened = ResearchDelivery(store).open(ref)
    assert opened["delivery"]["status"] == "not-declared"
    assert opened["context"]["research"]["body"] == "Old text" and opened["request"] is None


def test_history_pagination_is_exact_not_latest(store):
    payload = delivery_payload()
    first = ResearchDelivery(store).deliver(payload, submission_id="one")
    p2 = continued(store, payload, first)
    second = ResearchDelivery(store).deliver(p2, submission_id="two")
    p3 = continued(store, p2, second); p3["revision_id"] = "r3"
    third = ResearchDelivery(store).deliver(p3, submission_id="three")
    opened = ResearchDelivery(store).open(BlobRef(**third["reference"]), history_limit=1)
    assert opened["history"][0]["research"]["reference"] == second["reference"]
    assert opened["next_history_reference"] == first["reference"]
    tail = ResearchDelivery(store).open(BlobRef(**opened["next_history_reference"]))
    assert tail["context"]["research"]["reference"] == first["reference"]


def test_external_locator_remains_external_not_a_downloaded_original(store):
    payload = delivery_payload()
    payload["items"][2]["source"].update(custody="external", text=None, limitations=["Body not acquired"])
    payload["items"][2]["source"]["kind"] = "content"
    receipt = ResearchDelivery(store).deliver(payload, submission_id="external")
    view = ResearchDelivery(store).verify(BlobRef(**receipt["reference"]))
    assert view["items"][2]["representation"] == "external-only"
    assert view["sources"][0]["custody"] == "external" and view["source_authenticity"] == "not-certified"


def test_source_failure_is_not_zero_or_successful_complete_archive(store, monkeypatch):
    receipt = ResearchDelivery(store).deliver(delivery_payload(), submission_id="one")
    def unavailable(ref):
        raise StoreUnavailable("UNAVAILABLE")
    monkeypatch.setattr(store, "read_source", unavailable)
    result = ResearchDelivery(store).verify(BlobRef(**receipt["reference"]))
    assert result["status"] == "failed" and result["items"][0]["readback"] == "verified"
    assert result["sources"][0]["status"] == "unavailable"


def test_source_inventory_can_reuse_preexisting_progress_reference_without_duplication(store):
    payload = delivery_payload()
    first = ResearchDelivery(store).deliver(payload, submission_id="one")
    successor = continued(store, payload, first)
    predecessor = store.read_revision(BlobRef(**first["reference"]))
    successor["progress"]["sources"] = predecessor["record"]["input"]["sources"]
    saved = ResearchDelivery(store).deliver(successor, submission_id="two")
    record = store.read_revision(BlobRef(**saved["reference"]))["record"]["input"]
    assert len(record["sources"]) == 1
    assert ResearchDelivery(store).verify(BlobRef(**saved["reference"]))["status"] == "verified-declared-inventory"


def test_acquired_source_feeds_existing_numeric_audit_and_same_checkpoint(store):
    payload = delivery_payload()
    context = dict(subject="synthetic", period="2025-FY", scope="consolidated")
    payload["numerical_workpaper"] = {"schema": "research-numbers-v1", "inputs": [
        dict(id="sales", value="3", unit="CNY", source=0, locator={"row": 1, "column": "value"}, **context),
        dict(id="cash", value="2", unit="CNY", source=0, locator={"row": 2, "column": "value"}, **context)],
        "calculations": [dict(id="conversion", operation="ratio", operands=["cash", "sales"],
                               unit="percent", places=2, claimed="66.67", **context)],
        "template": "Synthetic conversion {{number:conversion}}"}
    saved = ResearchDelivery(store).deliver(payload, submission_id="numbers")
    assert saved["numerical_audit"]["status"] == "conditional"
    body = store.read_revision(BlobRef(**saved["reference"]))["body"]
    assert "66.67 percent" in body and "decision-os-numbers-v1:start" in body
    assert ResearchDelivery(store).verify(BlobRef(**saved["reference"]))["status"] == "verified-declared-inventory"


def timed_delivery_payload():
    payload = delivery_payload(method_id=EXTERNAL_METHOD)
    source = payload["items"][-1]["source"]
    source.update(published_at="2026-10-22T01:00:00Z", acquired_at="2026-10-23T01:00:00Z")
    context = dict(subject="synthetic", period="2025-FY", scope="consolidated")
    payload["numerical_workpaper"] = {
        "schema": "research-numbers-v2", "knowledge_cutoff": "2026-10-23T08:00:00Z",
        "evaluated_at": "2026-10-24T08:00:00Z", "inputs": [
            dict(id="cash", value="2", unit="CNY", source=0,
                 locator={"row": 2, "column": "value"}, **context)],
        "calculations": [dict(id="shown", operation="identity", operands=["cash"], unit="CNY", **context)],
        "template": "Synthetic cash {{number:shown}}"}
    return payload


@pytest.mark.parametrize("stored_at, expected", [
    ("2026-10-23T07:00:00Z", "conditional"),
    ("2026-10-23T09:00:00Z", "blocked"),
])
def test_v2_delivery_keeps_receipt_and_time_qualification_separate_through_replay(store, monkeypatch, stored_at, expected):
    from decision_kernel.application import versioned_store
    from decision_kernel.application.numerical_audit import NumericalAudit
    monkeypatch.setattr(versioned_store, "_now", lambda: stored_at)
    payload = timed_delivery_payload()
    saved = ResearchDelivery(store).deliver(payload, submission_id="timed-delivery")
    assert saved["numerical_audit"]["status"] == expected
    assert saved["numerical_audit"]["schema"] == "research-numerical-audit-v2"
    assert saved["numerical_audit"]["knowledge_cutoff"] == payload["numerical_workpaper"]["knowledge_cutoff"]
    assert saved["research_assessment"] == "not-performed" and saved["human_acceptance"] == "not-confirmed"
    fresh = lambda: VersionedStore(GitStore(store.backend.root))
    assert verify_fresh(saved, fresh)["readback"] == "fresh-reader-verified"
    ref = BlobRef(**saved["reference"])
    body = fresh().read_revision(ref)["body"]
    assert "decision-os-numbers-v2:start" in body
    audited = NumericalAudit(fresh()).saved(ref)
    assert audited["status"] == expected and audited["schema"] == "research-numerical-audit-v2"
    assert audited["knowledge_cutoff"] == payload["numerical_workpaper"]["knowledge_cutoff"]
    assert ResearchDelivery(fresh()).open(ref)["method"]["id"] == EXTERNAL_METHOD
    head = store.backend.head()
    monkeypatch.setattr(versioned_store, "_now", lambda: "2035-01-01T00:00:00Z")
    replay = ResearchDelivery(store).deliver(payload, submission_id="timed-delivery")
    assert replay["reference"] == saved["reference"]
    assert replay["numerical_audit"]["status"] == expected
    assert replay["numerical_audit"]["knowledge_cutoff"] == payload["numerical_workpaper"]["knowledge_cutoff"]
    assert replay["numerical_audit"]["evaluated_at"] == payload["numerical_workpaper"]["evaluated_at"]
    assert store.backend.head() == head and fresh().read_revision(ref)["body"] == body


@pytest.mark.parametrize("change", ["missing-time", "date-only", "naive-time", "reversed", "unsupported", "caller-qualified", "mixed-markers"])
def test_v2_delivery_bad_envelope_is_rejected_before_intent_or_source_write(store, change):
    payload = timed_delivery_payload()
    numeric = payload["numerical_workpaper"]
    if change == "missing-time":
        numeric.pop("evaluated_at")
    elif change == "date-only":
        numeric["knowledge_cutoff"] = "2026-10-23"
    elif change == "naive-time":
        numeric["evaluated_at"] = "2026-10-24T08:00:00"
    elif change == "reversed":
        numeric["evaluated_at"] = "2026-10-22T08:00:00Z"
    elif change == "unsupported":
        numeric["schema"] = "research-numbers-v99"
    elif change == "caller-qualified":
        numeric["qualified"] = True
    else:
        payload["progress"]["body"] += "\n\n<!-- decision-os-numbers-v2:start -->\n"
    head = store.backend.head()
    with pytest.raises(RecordError):
        ResearchDelivery(store).deliver(payload, submission_id="invalid-timed-delivery")
    assert store.backend.head() == head


@pytest.mark.parametrize("timed, metric, expected", [
    (False, "cash", "conditional"),
    (True, "cash", "conditional"),
    (False, "revenue", "blocked"),
])
def test_v3_delivery_retains_use_limits_and_exact_saved_result_through_replay(store, monkeypatch, timed, metric, expected):
    from decision_kernel.application import versioned_store
    from decision_kernel.application.numerical_audit import NumericalAudit
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-23T07:00:00Z")
    payload = timed_delivery_payload()
    numeric = payload["numerical_workpaper"]
    numeric["schema"] = "research-numbers-v3"
    if not timed:
        numeric.pop("knowledge_cutoff")
        numeric.pop("evaluated_at")
    numeric["inputs"][0]["evidence_use"] = dict(metric=metric, data_role="observed", security=None)
    numeric["inputs"][0]["evidence_bindings"] = {"metric": {"row": 2, "column": "metric"}}
    numeric["calculations"][0]["evidence_use"] = dict(metric=metric, data_role="derived", security=None)
    saved = ResearchDelivery(store).deliver(payload, submission_id="evidence-delivery")
    summary = saved["numerical_audit"]
    assert summary["schema"] == "research-numerical-audit-v3" and summary["status"] == expected
    assert summary["evidence_use_scope"] == "declared-source-fields-and-computation-lineage"
    assert summary["evidence_time_scope"] == ("retained-by-declared-cutoff" if timed else "not-requested")
    assert ("knowledge_cutoff" in summary) is timed
    fresh = lambda: VersionedStore(GitStore(store.backend.root))
    assert verify_fresh(saved, fresh)["readback"] == "fresh-reader-verified"
    reference = BlobRef(**saved["reference"])
    body = fresh().read_revision(reference)["body"]
    assert "decision-os-numbers-v3:start" in body
    audited = NumericalAudit(fresh()).saved(reference)
    assert audited["status"] == expected
    assert ResearchDelivery(fresh()).open(reference)["method"]["id"] == EXTERNAL_METHOD
    assert saved["research_assessment"] == "not-performed" and saved["human_acceptance"] == "not-confirmed"
    head = store.backend.head()
    monkeypatch.setattr(versioned_store, "_now", lambda: "2035-01-01T00:00:00Z")
    replay = ResearchDelivery(store).deliver(payload, submission_id="evidence-delivery")
    assert replay["reference"] == saved["reference"]
    assert replay["numerical_audit"]["status"] == expected
    assert replay["numerical_audit"]["evidence_use_scope"] == summary["evidence_use_scope"]
    assert replay["numerical_audit"]["evidence_time_scope"] == summary["evidence_time_scope"]
    assert ("knowledge_cutoff" in replay["numerical_audit"]) is timed
    assert store.backend.head() == head and fresh().read_revision(reference)["body"] == body


@pytest.mark.parametrize("field", ["knowledge_cutoff", "evaluated_at"])
def test_v3_delivery_partial_time_declaration_rejected_before_any_write(store, field):
    payload = timed_delivery_payload()
    payload["numerical_workpaper"]["schema"] = "research-numbers-v3"
    payload["numerical_workpaper"].pop(field)
    head = store.backend.head()
    with pytest.raises(RecordError):
        ResearchDelivery(store).deliver(payload, submission_id="partial-v3-times")
    assert store.backend.head() == head
