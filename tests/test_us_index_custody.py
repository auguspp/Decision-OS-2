"""US indices through the existing reading/custody path, using synthetic data only."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from decision_kernel.application.global_index_inputs import (
    US_INDEX_CODES, US_INDEX_READING_SCHEMA, read_us_index_context,
)
from decision_kernel.application.market_custody import (
    QUERY_SCHEMA, READING_SCHEMA, _manifest_source, market_reading_projection,
    read_stored_market, restore_market_query, retain_market_reading,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.observations.market_daily import MarketInputError
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR
from test_us_index_context import CALENDAR, START, END, source, index_rows


def payload():
    return {"schema": US_INDEX_READING_SCHEMA,
            "calendar_result": source("us_tradecal", CALENDAR),
            "index_results": {code: source("index_global", index_rows(code), code=code)
                              for code in US_INDEX_CODES},
            "declared_indices": list(US_INDEX_CODES), "start": START, "end": END,
            "horizons": [1, 3, 5]}


class USIndexCustodyTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend()
        self.store = VersionedStore(self.backend)
        self.data = payload()

    def fresh(self):
        return VersionedStore(MemoryBackend(self.backend.shared))

    def save(self, data=None, **kwargs):
        return retain_market_reading(self.data if data is None else data, REGISTRY, self.store,
            **{"fresh_reader": self.fresh, "public_rights_confirmed": True, **kwargs})

    def document(self, ref):
        return json.loads(self.fresh().read_source(SourceRef.from_dict(ref))["data"])

    def query(self, root_ref, code=None):
        refs = self.document(root_ref)["queries"].values()
        return next(ref for ref in refs
                    if self.document(ref)["use"]["parameters"].get("ts_code") == code)

    def read(self, ref, **kwargs):
        return read_stored_market(ref, REGISTRY, self.fresh(), **kwargs)

    def put_manifest(self, schema, data):
        return asdict(self.store.put_source("synthetic-tamper-" + digest(encoded(data)),
                                            _manifest_source(schema, data)))

    def test_shared_route_is_exact_original_reader_without_mutation(self):
        before = encoded(self.data)
        args = {k: v for k, v in self.data.items() if k != "schema"}
        args["horizons"] = tuple(args["horizons"])
        expected = read_us_index_context(**args, registry=REGISTRY)
        self.assertEqual(read_market_payload(self.data, REGISTRY), expected)
        self.assertEqual(encoded(self.data), before)
        for options in ({"limit": 1}, {"offset": 3}, {"entity_id": "SPX"}):
            with self.subTest(options=options):
                view = read_market_payload(self.data, REGISTRY, **options)
                self.assertEqual(view["view_version"], expected["view_version"])
                self.assertEqual(view["scope_count"], 4)
                self.assertEqual(view["source_qualification_counts"], expected["source_qualification_counts"])

    def test_new_schema_is_strict_not_a_diagnostic_or_world_route(self):
        variants = [[], {"schema": "market-source-check-v1", "observed_count": 4},
                    {**self.data, "exchange": "SSE"}, {**self.data, "benchmark": None},
                    {**self.data, "declared_indices": ["HSI"]},
                    {**self.data, "horizons": "5"}]
        for bad in variants:
            with self.subTest(kind=type(bad)), self.assertRaises((RecordError, MarketInputError)):
                self.save(bad)
        self.assertEqual(self.backend.shared["count"], 0)

    def test_full_multipage_roundtrip_uses_existing_root_and_query_formats(self):
        before = encoded(self.data)
        saved = self.save()
        self.assertEqual(saved["status"], "stored-and-independently-restored")
        self.assertEqual((saved["query_count"], saved["page_count"]), (5, 15))
        root = self.document(saved["reference"])
        self.assertEqual(root["schema"], READING_SCHEMA)
        self.assertEqual(root["reading"]["schema"], US_INDEX_READING_SCHEMA)
        self.assertNotIn('"items"', json.dumps(root))
        for ref in root["queries"].values():
            doc = self.document(ref)
            self.assertEqual(doc["schema"], QUERY_SCHEMA)
            self.assertNotIn('"items"', json.dumps(doc))
            restored = restore_market_query(ref, REGISTRY, self.fresh())
            self.assertFalse(restored["original_http_restored"])
            self.assertTrue(all(p["attempts"] == [] for p in restored["result"]["capture"]["pages"]))
        out = self.read(saved["reference"])
        self.assertEqual(encoded(out["view"]), encoded(read_market_payload(self.data, REGISTRY)))
        self.assertEqual(encoded(self.data), before)
        self.assertEqual(out["source_requests"], 0)
        self.assertEqual(out["view"]["unit"], "index-points")
        self.assertIsNone(out["view"]["currency"])

    def test_empty_and_absent_indices_keep_the_declared_denominator(self):
        self.data["index_results"].pop("RUT")
        self.data["index_results"]["DJI"] = source("index_global", [], code="DJI", empty_fields=True)
        saved = self.save()
        out = self.read(saved["reference"], limit=1)["view"]
        self.assertEqual(out["scope_count"], 4)
        self.assertEqual(out["source_qualification_counts"],
                         {"checked": 2, "checked-empty": 1, "not-supplied": 1, "rejected": 0})
        self.assertEqual(out["pagination"]["next_offset"], 1)
        rut = self.read(saved["reference"], entity_id="RUT")["view"]
        self.assertEqual(rut["view_version"], out["view_version"])
        self.assertIsNone(rut["entries"][0]["observation"])

    def test_lost_index_page_does_not_erase_peers_or_scope(self):
        saved = self.save()
        query = self.query(saved["reference"], "SPX")
        page = self.document(query)["pages"][1]["reference"]["manifest"]
        self.backend.shared["missing"].add((page["commit"], page["path"]))
        with patch("decision_kernel.infrastructure.relay_http.RelayHTTP.request",
                   side_effect=AssertionError("no source fallback")):
            out = self.read(saved["reference"])
        self.assertEqual((out["status"], out["unavailable_queries"]), ("partial-restoration", 1))
        self.assertEqual(out["view"]["scope_count"], 4)
        self.assertEqual(out["view"]["source_qualification_counts"],
                         {"checked": 3, "checked-empty": 0, "not-supplied": 0, "rejected": 1})
        self.assertEqual(out["view"]["observed_count"], 3)
        self.assertEqual([s["path"] for s in out["sources"] if s["status"] == "unavailable"],
                         [["index_results", "SPX"]])
        self.assertEqual(out["sources"][-1]["reason_code"], "OBJECT_MISSING")
        self.assertEqual(out["source_requests"], 0)

    def test_lost_calendar_makes_the_shared_view_unknown_not_zero(self):
        saved = self.save()
        manifest = self.query(saved["reference"])["manifest"]
        self.backend.shared["missing"].add((manifest["commit"], manifest["path"]))
        out = self.read(saved["reference"])
        self.assertEqual(out["status"], "partial-restoration")
        self.assertEqual(out["unavailable_queries"], 1)
        self.assertIsNone(out["view"])
        self.assertEqual(out["reading_error"], "MARKET_RELAY_RESULT_INVALID")
        self.assertEqual(sum(s["status"] == "restored" for s in out["sources"]), 4)

    def test_old_reference_survives_revision_and_same_intent_does_not_write(self):
        first = self.save()
        before = self.backend.head()
        self.assertEqual(self.save()["reference"], first["reference"])
        self.assertEqual(self.backend.head(), before)
        rows = index_rows(); rows[-1][-1] = "155"
        self.data["index_results"]["SPX"] = source("index_global", rows)
        second = self.save()
        self.assertNotEqual(second["reference"], first["reference"])
        self.assertNotEqual(second["restored"]["view"]["view_version"], first["restored"]["view"]["view_version"])
        self.assertEqual(self.read(first["reference"])["view"], first["restored"]["view"])

    def test_rights_and_independent_reader_precede_any_write(self):
        for options in ({"public_rights_confirmed": False}, {"public_rights_confirmed": "true"},
                        {"fresh_reader": lambda: self.store},
                        {"fresh_reader": lambda: VersionedStore(self.backend)}):
            with self.subTest(options=options), self.assertRaises(RecordError):
                self.save(**options)
            self.assertEqual(self.backend.shared["count"], 0)

    def test_incomplete_or_out_of_scope_captures_preflight_without_writes(self):
        incomplete = deepcopy(self.data)
        incomplete["index_results"]["SPX"] = source("index_global", index_rows(), max_pages=1)
        wrong = deepcopy(self.data)
        wrong["index_results"]["HSI"] = wrong["index_results"].pop("SPX")
        private = deepcopy(self.data)
        private["calendar_result"]["use"]["api"] = "p_list"
        for bad in (incomplete, wrong, private):
            with self.subTest(), self.assertRaises((RecordError, MarketInputError)):
                self.save(bad)
            self.assertEqual(self.backend.shared["count"], 0)

    def test_projection_and_retention_strip_attempts_not_source_columns(self):
        marker = "PRIVATE_EXECUTION_DIAGNOSTIC_ONLY"
        for result in [self.data["calendar_result"], *self.data["index_results"].values()]:
            result["capture"]["pages"][0]["attempts"] = [{"private": marker}]
        before = encoded(self.data)
        projected = market_reading_projection(self.data, REGISTRY)
        self.assertNotIn(marker, encoded(projected).decode())
        self.assertEqual(read_market_payload(projected, REGISTRY), read_market_payload(self.data, REGISTRY))
        saved = self.save()
        self.assertFalse(any(marker.encode() in raw
                             for raw in self.backend.shared["trees"][self.backend.head()].values()))
        self.assertEqual(self.read(saved["reference"])["view"], read_market_payload(projected, REGISTRY))
        self.assertEqual(encoded(self.data), before)

    def test_unknown_write_stops_then_reconciles_the_same_existing_journal(self):
        original = self.backend.commit
        calls = []
        def uncertain(files, *, expected):
            calls.append(files)
            original(files, expected=expected)
            raise UnknownWrite("REF_UPDATE_UNCONFIRMED")
        with patch.object(self.backend, "commit", side_effect=uncertain):
            with self.assertRaises(UnknownWrite):
                self.save()
        self.assertEqual(len(calls), 1)
        saved = self.save()
        head = self.backend.head()
        self.assertEqual(self.save()["reference"], saved["reference"])
        self.assertEqual(self.backend.head(), head)

    def test_registry_drift_cannot_silently_refresh_or_qualify_the_view(self):
        saved = self.save()
        changed = deepcopy(REGISTRY); changed.snapshot["sha256"] = "0" * 64
        out = read_stored_market(saved["reference"], changed, self.fresh())
        self.assertEqual(out["status"], "partial-restoration")
        self.assertEqual(out["unavailable_queries"], 5)
        self.assertIsNone(out["view"])
        self.assertTrue(all(s["reason_code"] == "MARKET_CUSTODY_REGISTRY_CHANGED" for s in out["sources"]))

    def test_query_and_page_substitution_are_rechecked_for_us_sources(self):
        saved = self.save()
        ref = self.query(saved["reference"], "SPX")
        base = self.document(ref)
        for mutation in ("duplicate", "order", "foreign", "scope"):
            data = deepcopy(base)
            if mutation == "duplicate":
                data["pages"][1] = deepcopy(data["pages"][0])
            elif mutation == "order":
                data["pages"].reverse()
            elif mutation == "foreign":
                data["pages"][0]["reference"]["manifest"]["repository"] = "synthetic-git:other"
            else:
                data["use"]["parameters"]["ts_code"] = "DJI"
            altered = self.put_manifest(QUERY_SCHEMA, data)
            with self.subTest(mutation=mutation), self.assertRaises((RecordError, MarketInputError)):
                restore_market_query(altered, REGISTRY, self.fresh())


class USIndexExistingHostTests(unittest.TestCase):
    def test_original_and_common_cli_then_new_process_reads_only_root_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = payload()
            input_path = root / "input.json"; input_path.write_bytes(encoded(data))
            legacy_path = root / "legacy.json"
            legacy_path.write_bytes(encoded({k: v for k, v in data.items() if k != "schema"}))
            env = {k: v for k, v in os.environ.items() if k not in
                   ("TUSHARE_PROXY_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "DECISION_OS_STORE_TOKEN")}
            env["PYTHONPATH"] = str(ROOT_DIR / "src")
            def cli(module, *args):
                out = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces." + module,
                    *args], cwd=tmp, env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(out.returncode, 0, out.stderr)
                return json.loads(out.stdout)
            registry = ["--registry", str(REGISTRY_PATH)]
            direct = cli("market_relay_cli", *registry, "--input", str(input_path))
            legacy = cli("global_index_cli", *registry, "--input", str(legacy_path))
            self.assertEqual(direct, legacy)
            store_path = root / "synthetic-records.git"; GitStore.init_synthetic(store_path)
            common = ["--path", str(store_path), *registry]
            saved = cli("market_custody_cli", "save", *common, "--input", str(input_path),
                        "--public-rights-confirmed")
            ref_path = root / "ref.json"; ref_path.write_bytes(encoded(saved["reference"]))
            input_path.unlink(); legacy_path.unlink()
            before = GitStore(store_path).head()
            restored = cli("market_custody_cli", "read", *common, "--input", str(ref_path),
                           "--limit", "1", "--entity-id", "SPX")
            self.assertEqual(restored["status"], "restored")
            self.assertEqual(restored["view"]["view_version"], direct["view_version"])
            self.assertEqual(restored["view"]["entries"], [e for e in direct["entries"] if e["entity_id"] == "SPX"])
            self.assertEqual(restored["view"]["scope_count"], 4)
            self.assertEqual(restored["source_requests"], 0)
            self.assertEqual(GitStore(store_path).head(), before)

    def test_original_runtime_uses_same_rights_gate_projection_and_argv(self):
        from decision_kernel.interfaces.market_custody_cli import run_runtime
        data = payload()
        data["index_results"]["SPX"]["capture"]["pages"][0]["attempts"] = [{"private": "NOT_RETAINED"}]
        binding = {"repository": "auguspp/Decision-OS-2", "repository_id": 1420041744,
                   "branch": "records", "anchor": "b" * 40, "store_id": "synthetic-test-binding",
                   "write_enabled": False, "visibility": "public"}
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GITHUB_REPOSITORY": "auguspp/Decision-OS-2", "GITHUB_REF": "refs/heads/main",
                   "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": "a" * 40,
                   "RUNNER_TEMP": tmp, "TUSHARE_PROXY_API_KEY": "not-forwarded"}
            event = {"inputs": {"operation": "save", "code_sha": "a" * 40,
                                "payload": json.dumps(data), "public_rights_confirmed": False}}
            calls = []
            def run(cmd, **kwargs):
                calls.append(cmd)
                self.assertNotIn("TUSHARE_PROXY_API_KEY", kwargs["env"])
                self.assertIn("--public-rights-confirmed", cmd)
                projected = json.loads((Path(tmp) / "out/input.json").read_text())
                self.assertNotIn("NOT_RETAINED", json.dumps(projected))
                self.assertEqual(read_market_payload(projected, REGISTRY)["scope_count"], 4)
                return subprocess.CompletedProcess(cmd, 0, '{"status":"synthetic-child-only"}', "")
            with patch("decision_kernel.interfaces.market_custody_cli._load", return_value=binding):
                with self.assertRaisesRegex(RecordError, "RUNTIME_AUTHORIZATION"):
                    run_runtime(event, env, Path(tmp) / "denied", run=run)
                self.assertEqual(calls, [])
                self.assertFalse((Path(tmp) / "denied").exists())
                event["inputs"]["public_rights_confirmed"] = True
                self.assertEqual(run_runtime(event, env, Path(tmp) / "out", run=run), 0)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
