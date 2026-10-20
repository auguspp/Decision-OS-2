"""Native reconciliation admission; every run, ref and ZIP here is synthetic.

These tests preserve a failed source attempt as failure while allowing its
independently settled writes to be reconciled. No provider data is retained.
"""
from copy import deepcopy
from io import BytesIO
import stat
import struct
import zipfile

import pytest

from decision_kernel.domain.records import RecordError, StoreUnavailable, digest, encoded
from decision_kernel.interfaces import daily_write_reconciliation_cli as cli


def _reference(number):
    return {"manifest": {"repository": "auguspp/Decision-OS-2", "commit": f"{number:040x}",
        "path": f"source-views/synthetic-native-proof-{number}/" + f"{number:064x}" + "/manifest.json",
        "sha256": f"{number + 10:064x}"},
        "selection": {"kind": "all", "start": None, "end": None, "columns": []}}


def _zip(documents, *, compression=zipfile.ZIP_DEFLATED, symlink=None):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, raw in documents.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = compression
            info.external_attr = (stat.S_IFLNK | 0o777 if name == symlink else stat.S_IFREG | 0o644) << 16
            archive.writestr(info, encoded(raw) if isinstance(raw, dict) else raw)
    return buffer.getvalue()


class NativeOriginal:
    def __init__(self):
        self.grant = {"schema": "daily-write-reconciliation-v1",
            "original_reference": _reference(5), "settled_market_reference": _reference(4),
            "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-1",
            "additional_source_requests": 0,
            "native_run": {"run_id": 9001, "run_attempt": 1, "code_sha": "a" * 40,
                "artifact_id": 9002, "sha256": "b" * 64, "size_bytes": 1},
            "submissions": [{"submission_id": "synthetic-original-" + str(n),
                "intent_sha256": f"{n + 20:064x}", "reference": _reference(n),
                "expected_head": f"{n - 1:040x}"} for n in range(1, 6)]}
        self.documents = {
            "result.json": {"schema": "daily-market-runtime-v1", "operation": "update",
                "invocation": "repair-calendar", "run_id": 9001, "run_attempt": 1,
                "code_sha": "a" * 40, "status": "persistence-unconfirmed",
                "reason_code": "REMOTE_WRITE_REQUIRES_RECONCILIATION", "source_requests": 1,
                "calendar_source_requests": 0, "price_source_requests": 1,
                "price_source_budget": 12, "calendar_source_budget": 3, "calendar_reserved_requests": 3,
                "cycle_date": "20261019", "cycle_status": "blocked", "expected_completed_session": "20261019"},
            "control.json": {"schema": "daily-market-reading-control-v1",
                "cycle_reference": _reference(5), "market_reference": _reference(4),
                "generation": "daily-20261019", "frontier_blocking_reference": _reference(4),
                "frontier_successor_references": [], "frontier_reason_code": "FTSHARE_SINGLE_BASE_NOT_TERMINAL"},
            "discovery-result.json": {"schema": "synthetic-discovery", "source_requests": 0},
            "reading-room.json": {"schema": "synthetic-room", "status": "partial"},
            "reading-room.html": b"<p>Synthetic reading with an unresolved source receipt.</p>",
            "reading-summary.md": b"Synthetic incomplete result; no provider or research content."}
        self.run = {"id": 9001, "run_attempt": 1, "head_sha": "a" * 40,
            "head_branch": "main", "path": ".github/workflows/daily-market.yml",
            "name": "decision-os-daily-market", "event": "workflow_dispatch",
            "status": "completed", "conclusion": "failure",
            "repository": {"id": 1420041744}, "head_repository": {"id": 1420041744}}
        self.meta = {"id": 9002, "name": "dos2-daily-market-9001-1", "expired": False,
            "workflow_run": {"id": 9001, "repository_id": 1420041744,
                "head_repository_id": 1420041744, "head_sha": "a" * 40, "head_branch": "main"}}
        steps = [("Advance at most two calendar requests and twelve FTShare whole-range requests", "failure"),
            ("Build daily reading from saved Brief, research and the actual market attempt", "success"),
            ("Recheck native visibility and publication authority before releasing exact references", "success"),
            ("Retain the authorized daily status and exact consumer input", "success")]
        self.jobs = {"total_count": 3, "jobs": [{"id": 9100 + i, "run_id": 9001,
            "run_attempt": 1, "head_sha": "a" * 40, "name": name, "status": "completed",
            "conclusion": conclusion, "steps": [{"number": n + 1, "name": step,
                "status": "completed", "conclusion": result} for n, (step, result) in enumerate(steps)]
                    if name == "daily" else []}
            for i, (name, conclusion) in enumerate((("visibility", "success"),
                ("daily", "failure"), ("status", "skipped")))]}
        self.commits = {row["reference"]["manifest"]["commit"]: {
            "sha": row["reference"]["manifest"]["commit"], "parents": [{"sha": row["expected_head"]}]}
            for row in self.grant["submissions"]}
        self.calls, self.downloads = [], []
        self.failure_at, self.failure = None, None
        self.refresh_archive()

    def refresh_archive(self, raw=None):
        self.raw = _zip(self.documents) if raw is None else raw
        self.grant["native_run"].update(sha256=digest(self.raw), size_bytes=len(self.raw))
        self.meta.update(digest="sha256:" + digest(self.raw), size_in_bytes=len(self.raw))

    def request(self, method, path, payload=None):
        assert method == "GET" and payload is None
        self.calls.append((method, path))
        if self.failure_at == len(self.calls):
            raise self.failure
        if path == cli.PREFIX + "/actions/runs/9001/attempts/1":
            return deepcopy(self.run)
        if path == cli.PREFIX + "/actions/artifacts/9002":
            return deepcopy(self.meta)
        if path == cli.PREFIX + "/actions/runs/9001/attempts/1/jobs?per_page=100":
            return deepcopy(self.jobs)
        if path.startswith(cli.PREFIX + "/git/commits/"):
            return deepcopy(self.commits[path.rsplit("/", 1)[1]])
        raise AssertionError("Unexpected native request")

    def download(self, repository, artifact_id):
        self.downloads.append((repository, artifact_id))
        assert (repository, artifact_id) == ("auguspp/Decision-OS-2", 9002)
        return self.raw

    def verify(self):
        return cli.verify_native_reconciliation(self, self.grant, archive_loader=self.download)


def test_complete_native_and_archive_proof_retains_original_failure_without_any_write():
    native = NativeOriginal()
    before = deepcopy(native.documents)
    result = native.verify()
    assert result["status"] == "verified"
    assert result["original_conclusion"] == "failure"
    assert result["original_status"] == "persistence-unconfirmed"
    assert result["original_reason_code"] == "REMOTE_WRITE_REQUIRES_RECONCILIATION"
    assert result["original_source_requests"] == 1 and result["source_requests"] == 0
    assert result["records_write"] == "not-requested" and result["verified_submission_count"] == 5
    assert len(native.calls) == 8 and len({path for _, path in native.calls}) == 8
    assert native.downloads == [("auguspp/Decision-OS-2", 9002)]
    assert native.documents == before


@pytest.mark.parametrize("change", [
    {"conclusion": "success"}, {"run_attempt": 2}, {"run_attempt": True},
    {"head_sha": "b" * 40}, {"head_branch": "feature"}, {"status": "in_progress"},
    {"path": ".github/workflows/other.yml"}, {"event": "schedule"},
    {"head_repository": {"id": 1420041745}},
])
def test_original_run_identity_mismatch_stops_before_artifact_reads(change):
    native = NativeOriginal()
    native.run.update(change)
    with pytest.raises(RecordError, match="NATIVE_RUN"):
        native.verify()
    assert len(native.calls) == 1 and native.downloads == []


@pytest.mark.parametrize("change", [
    {"expired": True}, {"expired": 0}, {"size_in_bytes": 0},
    {"digest": "sha256:" + "b" * 64}, {"name": "dos2-daily-market-other-1"},
    {"workflow_run": {"id": 9001, "repository_id": 1420041744,
        "head_repository_id": 1420041744, "head_sha": "b" * 40, "head_branch": "main"}},
])
def test_artifact_expiry_binding_or_digest_mismatch_prevents_download(change):
    native = NativeOriginal()
    native.meta.update(change)
    with pytest.raises(RecordError, match="NATIVE_ARTIFACT"):
        native.verify()
    assert len(native.calls) == 2 and native.downloads == []


@pytest.mark.parametrize("change", ["attempt", "head", "source-success", "reading-failed", "release-skipped", "duplicate-step", "missing-job"])
def test_native_job_and_step_evidence_cannot_be_borrowed_or_relabelled(change):
    native = NativeOriginal()
    daily = native.jobs["jobs"][1]
    if change == "attempt": daily["run_attempt"] = True
    elif change == "head": daily["head_sha"] = "b" * 40
    elif change == "source-success": daily["steps"][0]["conclusion"] = "success"
    elif change == "reading-failed": daily["steps"][1]["conclusion"] = "failure"
    elif change == "release-skipped": daily["steps"][2]["status"] = daily["steps"][2]["conclusion"] = "skipped"
    elif change == "duplicate-step": daily["steps"].append(deepcopy(daily["steps"][0]))
    else: native.jobs["jobs"].pop()
    with pytest.raises(RecordError, match="NATIVE_(JOBS|STEPS)"):
        native.verify()
    assert len(native.calls) == 3 and native.downloads == []


@pytest.mark.parametrize("change", ["wrong-parent", "two-parents", "wrong-commit"])
def test_original_commit_parent_is_native_and_cannot_be_supplied_by_journal(change):
    native = NativeOriginal()
    second = native.commits[f"{2:040x}"]
    if change == "wrong-parent": second["parents"] = [{"sha": "f" * 40}]
    elif change == "two-parents": second["parents"].append({"sha": "f" * 40})
    else: second["sha"] = "f" * 40
    with pytest.raises(RecordError, match="NATIVE_PARENT"):
        native.verify()
    assert len(native.calls) == 5 and native.downloads == []


@pytest.mark.parametrize("code", ["REMOTE_NOT_AUTHORIZED", "REMOTE_RATE_LIMIT", "REMOTE_RESPONSE_UNAVAILABLE"])
def test_native_denial_or_transport_failure_is_not_retried_or_replaced(code):
    native = NativeOriginal()
    original = StoreUnavailable(code)
    native.failure_at, native.failure = 2, original
    with pytest.raises(StoreUnavailable) as caught:
        native.verify()
    assert caught.value is original
    assert len(native.calls) == 2 and native.downloads == []


@pytest.mark.parametrize("change", ["append", "alter-byte"])
def test_downloaded_original_bytes_must_match_fixed_reviewed_hash(change):
    native = NativeOriginal()
    native.raw = native.raw + b"x" if change == "append" else bytes([native.raw[0] ^ 1]) + native.raw[1:]
    with pytest.raises(RecordError, match="ARCHIVE_DIGEST"):
        native.verify()
    assert len(native.calls) == 8 and len(native.downloads) == 1


@pytest.mark.parametrize("change", [
    {"status": "complete"}, {"reason_code": "FTSHARE_HTTP_429"},
    {"source_requests": 0}, {"source_requests": 2}, {"source_requests": True},
    {"price_source_budget": 13}, {"calendar_source_requests": 1},
    {"run_attempt": 2}, {"invocation": "schedule"}, {"cycle_date": "20261020"},
])
def test_original_result_does_not_reset_used_requests_or_rewrite_failed_attempt(change):
    native = NativeOriginal()
    native.documents["result.json"].update(change)
    native.refresh_archive()
    with pytest.raises(RecordError, match="ORIGINAL_RESULT"):
        native.verify()
    assert len(native.downloads) == 1


@pytest.mark.parametrize("change", ["cycle", "market", "generation", "frontier", "successor"])
def test_archive_control_is_bound_to_original_blocked_and_settled_references(change):
    native = NativeOriginal()
    control = native.documents["control.json"]
    if change == "cycle": control["cycle_reference"] = _reference(3)
    elif change == "market": control["market_reference"] = _reference(3)
    elif change == "generation": control["generation"] = "daily-20261020"
    elif change == "frontier": control["frontier_blocking_reference"] = None
    else: control["frontier_successor_references"] = [_reference(3)]
    native.refresh_archive()
    with pytest.raises(RecordError, match="ORIGINAL_CONTROL"):
        native.verify()
    assert len(native.downloads) == 1


@pytest.mark.parametrize("change", ["traversal", "missing", "symlink", "encrypted", "unsupported-compression", "expanded-bound"])
def test_unsafe_or_unbounded_archives_never_supply_a_reconciliation(change):
    native = NativeOriginal()
    if change == "traversal":
        native.documents["../control.json"] = native.documents.pop("control.json")
        raw = _zip(native.documents)
    elif change == "missing":
        native.documents.pop("reading-summary.md")
        raw = _zip(native.documents)
    elif change == "symlink": raw = _zip(native.documents, symlink="reading-summary.md")
    elif change == "unsupported-compression": raw = _zip(native.documents, compression=zipfile.ZIP_BZIP2)
    elif change == "expanded-bound":
        native.documents["reading-room.html"] = b"x" * (8 * 1024 * 1024)
        raw = _zip(native.documents)
    else:
        raw = bytearray(_zip(native.documents))
        local = raw.index(b"PK\x03\x04")
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, local + 6, struct.unpack_from("<H", raw, local + 6)[0] | 1)
        struct.pack_into("<H", raw, central + 8, struct.unpack_from("<H", raw, central + 8)[0] | 1)
        raw = bytes(raw)
    native.refresh_archive(raw)
    with pytest.raises(RecordError, match="ARCHIVE_MEMBERS"):
        native.verify()
    assert len(native.downloads) == 1


def test_bad_crc_and_invalid_zip_are_safe_errors_without_retries():
    native = NativeOriginal()
    raw = bytearray(_zip(native.documents, compression=zipfile.ZIP_STORED))
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        info = archive.getinfo("reading-summary.md")
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    raw[offset] ^= 1
    native.refresh_archive(bytes(raw))
    with pytest.raises(RecordError, match="ARCHIVE_CRC"):
        native.verify()
    assert len(native.downloads) == 1
    invalid = NativeOriginal()
    invalid.refresh_archive(b"Synthetic invalid ZIP")
    with pytest.raises(RecordError, match="ARCHIVE_INVALID"):
        invalid.verify()
    assert len(invalid.downloads) == 1
