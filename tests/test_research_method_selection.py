"""Trusted method selection preserves default bytes and cannot approve supplied code."""
from contextlib import redirect_stdout
from io import StringIO
import json
import unittest

from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.methods.research import (
    EXTERNAL_METHOD_ID, continuation_method, main, research_method,
    stored_research_method, writable_research_method,
)

QUICK_V1 = "c2186dad0da13b8ea6fac3195640991c4b2fbf5c3dc284655dd39d176d45b764"
FULL_V1 = "1626dd595adfbc731c674080d54fca04072e0b99b843d0c5e37d3a276f4a0138"
FULL_V2 = "6509212be4fa216b6d2333ffa1a2568b4b7e556070d390d19939eb382ededc62"
EXTERNAL_FULL_V1 = "e3d52b9cd39885fb5a05cc86f5573e257d4b59f6c3878b9b87eecdb1b31485ef"


def token(method):
    return method["id"] + "@sha256:" + method["sha256"]


class ResearchMethodSelectionTests(unittest.TestCase):
    def test_default_and_historical_method_bytes_remain_exact(self):
        for kind, expected in (("quick", QUICK_V1), ("full", FULL_V2)):
            with self.subTest(kind=kind):
                method = research_method(kind)
                self.assertEqual(method["sha256"], expected)
                self.assertEqual(digest(encoded({k: v for k, v in method.items() if k != "sha256"})), expected)
        old = research_method("full", sha256=FULL_V1)
        self.assertEqual(old["id"], "decision-os-research-full-v1")
        self.assertEqual(digest(encoded({k: v for k, v in old.items() if k != "sha256"})), FULL_V1)

    def test_external_identity_is_explicit_and_returns_fresh_definition(self):
        selected = research_method("full", method_id=EXTERNAL_METHOD_ID)
        sha = EXTERNAL_FULL_V1
        self.assertEqual(selected["sha256"], sha)
        self.assertEqual(digest(encoded({k: v for k, v in selected.items() if k != "sha256"})), sha)
        self.assertEqual(research_method("full", sha256=sha), selected)
        self.assertEqual(selected["source_use_contract"], "research-external-source-use-audit-v1")
        self.assertEqual(selected["assessment"], "guidance-only-not-economic-certification")
        selected["steps"].clear()
        self.assertTrue(research_method("full", method_id=EXTERNAL_METHOD_ID)["steps"])
        self.assertEqual(research_method("full")["sha256"], FULL_V2)

    def test_new_write_allowlist_does_not_restore_archived_or_foreign_method(self):
        external = research_method("full", method_id=EXTERNAL_METHOD_ID)
        for kind, sha in (("quick", QUICK_V1), ("full", FULL_V2), ("full", external["sha256"])):
            with self.subTest(kind=kind, sha=sha):
                self.assertEqual(writable_research_method(kind, sha)["sha256"], sha)
        for kind, sha in (("full", FULL_V1), ("quick", external["sha256"]),
                          ("full", QUICK_V1), ("full", "0" * 64), ("full", None), ("full", {})):
            with self.subTest(kind=kind, sha=sha):
                with self.assertRaisesRegex(RecordError, "^RESEARCH_METHOD_CHANGED_REPREPARE$"):
                    writable_research_method(kind, sha)

    def test_unknown_ids_and_ambiguous_selectors_cannot_fall_back_to_default(self):
        for kind, ident in (("quick", EXTERNAL_METHOD_ID), ("full", "decision-os-research-full-v1"),
                            ("full", "external"), ("full", {"approved": True})):
            with self.subTest(kind=kind, ident=ident):
                with self.assertRaisesRegex(RecordError, "^UNKNOWN_RESEARCH_METHOD$"):
                    research_method(kind, method_id=ident)
        with self.assertRaisesRegex(RecordError, "^ONE_RESEARCH_METHOD_SELECTOR_REQUIRED$"):
            research_method("full", sha256=FULL_V2, method_id=EXTERNAL_METHOD_ID)

    def test_stored_identity_requires_kind_id_and_digest_but_unknown_records_remain_readable(self):
        external = research_method("full", method_id=EXTERNAL_METHOD_ID)
        for method in (research_method("full"), external, research_method("full", sha256=FULL_V1)):
            with self.subTest(method=method["id"]):
                self.assertEqual(stored_research_method("full", token(method)), method)
        for value in ("old free-form method", EXTERNAL_METHOD_ID, EXTERNAL_METHOD_ID + "@sha256:" + FULL_V2,
                      "forged@sha256:" + external["sha256"], token(external) + " ", None, {}):
            with self.subTest(value=value):
                self.assertIsNone(stored_research_method("full", value))
        self.assertIsNone(stored_research_method("quick", token(external)))

    def test_implicit_continuation_preserves_only_exact_external_contract(self):
        external = research_method("full", method_id=EXTERNAL_METHOD_ID)
        self.assertEqual(continuation_method("full", token(external)), external)
        self.assertEqual(continuation_method("full", token(research_method("full", sha256=FULL_V1)))["sha256"], FULL_V2)
        self.assertEqual(continuation_method("full", "unknown method")["sha256"], FULL_V2)
        self.assertEqual(continuation_method("quick", token(external))["sha256"], QUICK_V1)
        self.assertEqual(continuation_method("full", token(external),
                                             method_id="decision-os-research-full-v2")["sha256"], FULL_V2)

    def test_offline_method_cli_supports_explicit_external_contract(self):
        output = StringIO()
        with redirect_stdout(output):
            result = main(["--kind", "full", "--method-id", EXTERNAL_METHOD_ID])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["id"], EXTERNAL_METHOD_ID)


if __name__ == "__main__":
    unittest.main()
