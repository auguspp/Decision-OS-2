"""Real Git regressions for equivalent source references and lost delivery receipts."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from decision_kernel.application.research_delivery import ResearchDelivery, delivery_manifest, stage_key
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, Conflict, SourceInput, SourceRef, StoreUnavailable, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.domain.research_work import continue_request, read_workpaper
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.methods.research import research_method


class DeliverySourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "records.git"
        self.store = VersionedStore(GitStore.init_synthetic(self.root))
        self.source = self.store.put_source("source", SourceInput(
            "synthetic-source", "dataset", "Synthetic figures", "synthetic:source",
            "metric,value\nrevenue,3\ncash,2\n"))
        self.reference = json.loads(encoded(asdict(self.source)))

    def payload(self, *, kind="full", case="source-identity"):
        return {"schema": "research-delivery-v1", "rights_confirmed": True,
            "rights_basis": "Synthetic engineering fixtures only.", "body_mode": "snapshot",
            "request": {"case_id": case, "kind": kind, "mandate": "Synthetic source continuity",
                "questions": [{"question_id": "cash", "text": "How does profit become cash?"}]},
            "progress": {"body": "Synthetic original research.", "stop": "continue",
                "stop_reason": "Evidence still needs economic analysis.", "sources": [deepcopy(self.reference)],
                "findings": [{"question_id": "cash", "status": "open", "finding": "Figures acquired.",
                    "remaining_kind": "acquired-unanalysed", "next_action": "Analyse the cash bridge.",
                    "evidence": [0]}],
                "challenges": [{"question_id": "cash", "argument": "Expansion may explain the difference.",
                    "disposition": "unresolved", "response": "Retain pending analysis.", "evidence": [0]}]},
            "revision_id": "first", "method_sha256": research_method(kind)["sha256"],
            "items": [
                {"id": "report", "role": "report-original", "status": "include", "required": True,
                    "reason": "", "artifact": asdict(ArtifactInput.from_bytes("synthetic-report", "report.html",
                        "text/html", b"<!doctype html>\r\n<p>Synthetic original.</p>\r\n"))},
                {"id": "model", "role": "calculation-file", "status": "not-applicable", "required": False,
                    "reason": "No separate model in this synthetic mandate."},
                {"id": "figures", "role": "source-evidence", "status": "include", "required": True,
                    "reason": "", "source_ref": deepcopy(self.reference)}]}

    def successor(self, payload, receipt):
        previous = self.store.read_revision(BlobRef(**receipt["reference"]))
        result = deepcopy(payload)
        result["request"] = asdict(continue_request(previous, BlobRef(**receipt["reference"])))
        result["revision_id"] = "second"
        result["body_mode"] = "delta"
        result["progress"]["body"] = "Synthetic custody supplement; keep the original research."
        for item, old in zip(result["items"], delivery_manifest(previous["body"])["items"]):
            if "artifact" in item:
                item.pop("artifact")
                item["artifact_ref"] = old["reference"]
        return result

    def test_abbreviated_references_keep_source_order_and_evidence_indices(self):
        other = self.store.put_source("other-source", SourceInput(
            "other-source", "dataset", "Other figures", "synthetic:other", "metric,value\nrevenue,9\n"))
        expected = json.loads(encoded([asdict(other), asdict(self.source)]))
        variants = ({"manifest": self.reference["manifest"]},
                    {"manifest": self.reference["manifest"], "selection": {}},
                    {"manifest": self.reference["manifest"], "selection": {"kind": "all"}})
        for number, shortened in enumerate(variants):
            with self.subTest(selection=shortened.get("selection", "omitted")):
                first_payload = self.payload(case=f"source-identity-{number}")
                first_payload["progress"]["sources"] = deepcopy(expected)
                for row in first_payload["progress"]["findings"] + first_payload["progress"]["challenges"]:
                    row["evidence"] = [1]
                first = ResearchDelivery(self.store).deliver(first_payload, submission_id=f"first-{number}")
                payload = self.successor(first_payload, first)
                payload["progress"]["sources"][1] = shortened
                context = dict(subject="synthetic", period="2025-FY", scope="consolidated")
                payload["numerical_workpaper"] = {"schema": "research-numbers-v1", "inputs": [
                    dict(id="sales", value="3", unit="CNY", source=1, locator={"row": 1, "column": "value"}, **context),
                    dict(id="cash", value="2", unit="CNY", source=1, locator={"row": 2, "column": "value"}, **context)],
                    "calculations": [dict(id="conversion", operation="ratio", operands=["cash", "sales"],
                        unit="percent", places=2, claimed="66.67", **context)]}
                original = deepcopy(payload)
                saved = ResearchDelivery(self.store).deliver(payload, submission_id=f"second-{number}")
                fresh = VersionedStore(GitStore(self.root))
                research = fresh.read_revision(BlobRef(**saved["reference"]))
                self.assertEqual(research["record"]["input"]["sources"], expected)
                self.assertEqual(delivery_manifest(research["body"])["items"][2]["source_index"], 1)
                work = read_workpaper(research)[1]
                self.assertEqual(work.findings[0].evidence, (1,))
                self.assertEqual(work.challenges[0].evidence, (1,))
                self.assertEqual(saved["numerical_audit"]["status"], "conditional")
                self.assertEqual(saved["numerical_audit"]["failures"], [])
                self.assertEqual(saved["delivery_input_sha256"], digest(encoded(original)))
                self.assertEqual(payload, original)
                self.assertEqual(ResearchDelivery(fresh).verify(BlobRef(**saved["reference"]))["status"],
                                 "verified-declared-inventory")

    def test_different_selections_remain_distinct_sources(self):
        payload = self.payload()
        selected = {"manifest": self.reference["manifest"],
                    "selection": {"kind": "rows", "start": 1, "end": 1}}
        payload["progress"]["sources"] = [selected]
        payload["progress"]["findings"][0]["evidence"] = [0, 1]
        saved = ResearchDelivery(self.store).deliver(payload, submission_id="selections")
        research = self.store.read_revision(BlobRef(**saved["reference"]))
        self.assertEqual(research["record"]["input"]["sources"],
                         json.loads(encoded([asdict(SourceRef.from_dict(selected)), asdict(self.source)])))
        self.assertEqual(delivery_manifest(research["body"])["items"][2]["source_index"], 1)

    def test_same_original_payload_resumes_reserved_intent_without_mixing(self):
        payload = self.payload()
        payload["progress"]["sources"] = [{"manifest": self.reference["manifest"]}]
        original = deepcopy(payload)
        before = self.store.backend.head()
        with patch.object(self.store, "read_source", side_effect=StoreUnavailable("SYNTHETIC_INTERRUPTION")):
            with self.assertRaisesRegex(StoreUnavailable, "SYNTHETIC_INTERRUPTION"):
                ResearchDelivery(self.store).deliver(payload, submission_id="resume-original")
        interrupted = self.store.backend.head()
        self.assertNotEqual(interrupted, before)
        self.assertIsNone(self.store.current(payload["request"]["case_id"]))
        journal_path = "submissions/" + stage_key("resume-original", "intent") + ".json"
        journal = self.store.backend.read(interrupted, journal_path)
        changed = deepcopy(payload)
        changed["progress"]["body"] += " Changed input."
        with self.assertRaisesRegex(Conflict, "SUBMISSION_CONTENT_CONFLICT"):
            ResearchDelivery(self.store).deliver(changed, submission_id="resume-original")
        self.assertEqual(self.store.backend.head(), interrupted)
        saved = ResearchDelivery(self.store).deliver(payload, submission_id="resume-original")
        self.assertEqual(saved["delivery_input_sha256"], digest(encoded(original)))
        self.assertEqual(self.store.backend.read(self.store.backend.head(), journal_path), journal)
        self.assertEqual(payload, original)
        head = self.store.backend.head()
        replay = ResearchDelivery(VersionedStore(GitStore(self.root))).deliver(payload, submission_id="resume-original")
        self.assertEqual(replay["reference"], saved["reference"])
        self.assertEqual(self.store.backend.head(), head)

    def test_full_replay_keeps_pending_audit_bound_to_old_revision(self):
        payload = self.payload()
        first = ResearchDelivery(self.store).deliver(payload, submission_id="first")
        expected = deepcopy(first["source_use_acceptance"])
        self.assertEqual(expected["status"], "not-demonstrated")
        self.assertEqual(expected["operation"], "audit-source-use")
        self.assertEqual(expected["reference"], first["reference"])
        second = ResearchDelivery(self.store).deliver(self.successor(payload, first), submission_id="second")
        head = self.store.backend.head()
        replay = ResearchDelivery(VersionedStore(GitStore(self.root))).deliver(payload, submission_id="first")
        self.assertEqual(replay["source_use_acceptance"], expected)
        self.assertEqual(replay["reference"], first["reference"])
        self.assertEqual(self.store.current(payload["request"]["case_id"]), BlobRef(**second["reference"]))
        self.assertEqual(self.store.backend.head(), head)

    def test_quick_replay_does_not_add_full_source_requirements(self):
        payload = self.payload(kind="quick")
        first = ResearchDelivery(self.store).deliver(payload, submission_id="quick")
        head = self.store.backend.head()
        replay = ResearchDelivery(self.store).deliver(payload, submission_id="quick")
        self.assertNotIn("source_use_acceptance", first)
        self.assertNotIn("source_use_acceptance", replay)
        self.assertEqual(replay["reference"], first["reference"])
        self.assertEqual(self.store.backend.head(), head)


if __name__ == "__main__":
    unittest.main()
