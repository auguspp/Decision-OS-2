"""Retained evidence for the finite Kernel v1 daily study, without source I/O.

The trusted host authenticates the native run and artifact. This owner binds its
exact ZIP bytes to that proof, replays all four responses, and keeps the archive
in the existing SourceInput store so ordinary reading needs no Actions artifact.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
import stat
import zipfile

from decision_kernel.application.ftshare_single import capture_single, single_request
from decision_kernel.application.market_custody import _document, _read_source, market_read_scope
from decision_kernel.domain.records import RecordError, Selection, SourceInput, SourceRef, digest, encoded, require

EVIDENCE_SCHEMA = "ftshare-kernel-adoption-evidence-v1"
ADOPTION_PLAN_SCHEMA = "ftshare-kernel-adoption-plan-v1"
REPOSITORY = "auguspp/Decision-OS-2"
STUDY = "ftshare-daily-qualification-20261018"
MAX_ARCHIVE_BYTES = 512 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024
ROLES = ("raw-sz-history", "raw-sz-repeat", "raw-sh-history", "forward-sz-history")
PRICE_ROLES = ("raw-sz-history", "raw-sh-history", "forward-sz-history")
PRIOR_IDENTITY = {
    "artifact_bytes": 69318, "artifact_id": 12143465795,
    "artifact_sha256": "92bd239a077025ef24633b9050d8b859cc9233bfb650379a2be4c59eb85cd725",
    "code_sha": "64b4d5f6bfe0f9373a8e7f33f590979178de22e3", "cumulative_source_requests": 2,
    "outcome": "second-v2-first-request-http-403", "run_attempt": 1,
    "run_id": 39145109553, "source_requests": 1,
}


def _require(condition, code):
    require(condition, "FTSHARE_ADOPTION_" + code)


def _same(left, right):
    return encoded(left) == encoded(right)


def validate_adoption_proof(proof):
    _require(isinstance(proof, dict) and set(proof) == {
        "run_id", "run_attempt", "code_sha", "artifact_id", "sha256", "size_in_bytes"}, "PROOF")
    _require(all(type(proof[key]) is int and 0 < proof[key] < 10 ** 15
                 for key in ("run_id", "artifact_id"))
        and type(proof["run_attempt"]) is int and proof["run_attempt"] == 1
        and type(proof["size_in_bytes"]) is int and 0 < proof["size_in_bytes"] <= MAX_ARCHIVE_BYTES
        and isinstance(proof["code_sha"], str) and re.fullmatch(r"[a-f0-9]{40}", proof["code_sha"])
        and isinstance(proof["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", proof["sha256"]), "PROOF")
    return deepcopy(proof)


def kernel_check_plan():
    return [{"id": role, "symbol": symbol, "since": 1782835200000, "until": 1792166399999,
             "adjust_kind": adjustment}
        for role, symbol, adjustment in ((ROLES[0], "000001.SZ", "none"),
            (ROLES[1], "000001.SZ", "none"), (ROLES[2], "600000.SH", "none"),
            (ROLES[3], "000001.SZ", "forward"))]


def kernel_price_requests():
    return {spec["id"]: single_request(**{key: value for key, value in spec.items() if key != "id"})
            for spec in kernel_check_plan() if spec["id"] in PRICE_ROLES}


def _time(value):
    _require(isinstance(value, str) and "T" in value and len(value) <= 80, "CLOCK")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise RecordError("FTSHARE_ADOPTION_CLOCK") from None
    _require(result.utcoffset() is not None, "CLOCK")
    return result


def _price(value):
    _require(isinstance(value, str), "BASELINE_CLOSE")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise RecordError("FTSHARE_ADOPTION_BASELINE_CLOSE") from None
    _require(result.is_finite() and result > 0, "BASELINE_CLOSE")
    return result


def read_kernel_archive(archive, proof):
    """Rebuild the four exact captures from the bounded, native-hash-bound ZIP."""
    proof = validate_adoption_proof(proof)
    _require(type(archive) is bytes and len(archive) == proof["size_in_bytes"]
             and digest(archive) == proof["sha256"], "ARCHIVE_CHANGED")
    controls = {"baseline.json", "request.json", "result.json", "verification.json", "predecessor.json"}
    members = controls | {role + suffix for role in ROLES for suffix in (".raw.json", ".capture.json")}
    try:
        with zipfile.ZipFile(BytesIO(archive)) as source:
            entries = source.infolist()
            _require(len(entries) == len(members) and {entry.filename for entry in entries} == members,
                     "ARCHIVE_MEMBERS")
            _require(sum(entry.file_size for entry in entries) <= MAX_EXPANDED_BYTES
                and all(0 < entry.file_size <= MAX_ARCHIVE_BYTES and not entry.is_dir()
                    and not entry.flag_bits & 1 and not stat.S_ISLNK(entry.external_attr >> 16)
                    and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    for entry in entries), "ARCHIVE_BOUND")
            raw = {entry.filename: source.read(entry) for entry in entries}
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError):
        raise RecordError("FTSHARE_ADOPTION_ARCHIVE_INVALID") from None
    docs = {name: _document(raw[name]) for name in controls}
    _require(all(isinstance(value, dict) for value in docs.values()), "DOCUMENTS")
    request, report = docs["request.json"], docs["result.json"]
    predecessor, baseline, verification = (docs[name] for name in
        ("predecessor.json", "baseline.json", "verification.json"))
    identity = {"repository": REPOSITORY, "study": STUDY, "route": "kernel-v1", "route_budget": 4,
                "source_budget": 6, "run_id": str(proof["run_id"]), "attempt": 1, "code_sha": proof["code_sha"]}
    _require(set(predecessor) == {"identity", "documents"}
        and _same(predecessor["identity"], PRIOR_IDENTITY)
        and isinstance(predecessor["documents"], dict)
        and predecessor["documents"].get("baseline.json") == baseline, "PREDECESSOR")
    expected = {**identity, "plan": kernel_check_plan(), "predecessor": PRIOR_IDENTITY,
        "predecessor_sha256": digest(encoded(predecessor)), "baseline_sha256": digest(encoded(baseline)),
        "verification_sha256": digest(encoded(verification))}
    _require(_same(request, expected), "REQUEST_CHANGED")
    _require(_same({key: report.get(key) for key in identity}, identity)
        and report.get("schema") == "ftshare-kernel-daily-check-v1" and report.get("status") == "validated"
        and report.get("raw_release") == "private-artifact-only"
        and _same(report.get("predecessor"), PRIOR_IDENTITY), "RESULT")
    counts = {"source_requests": 4, "reserved_requests": 4, "prior_source_requests": 2,
        "cumulative_source_requests": 6, "remaining_source_requests": 0,
        "required_candle_checks": 4, "qualified_candle_checks": 4}
    _require(_same({key: report.get(key) for key in counts}, counts)
        and all(report.get(key) is False for key in ("records_written", "active_source_changed",
            "full_market_coverage_certified", "long_term_uptime_certified",
            "factor_qualification_granted", "account_daily_limit_verified")), "SPENDING_OR_CLAIMS")
    _require(set(verification) == {"calendar_references", "close", "daily_reference", "page_references",
        "scope_count", "sessions"} and isinstance(verification["sessions"], dict)
        and set(verification["sessions"]) == {"SSE", "SZSE"}
        and isinstance(verification["close"], dict)
        and set(verification["close"]) == {"000001.SZ", "600000.SH"}, "VERIFICATION")
    _require(isinstance(report.get("results"), list) and len(report["results"]) == 4, "RESULT_ROWS")
    started, completed = _time(report.get("started_at")), _time(report.get("completed_at"))
    captures = {}
    for spec, row in zip(kernel_check_plan(), report["results"], strict=True):
        role = spec["id"]
        _require(isinstance(row, dict) and row.get("id") == role
            and row.get("status") == "qualified-response" and type(row.get("source_requests")) is int
            and row["source_requests"] == 1 and row.get("raw_file") == role + ".raw.json", "RESULT_ROW")
        recorded = _document(raw[role + ".capture.json"])
        _require(isinstance(recorded, dict), "CAPTURE")
        capture = capture_single(raw[role + ".raw.json"], acquired_at=recorded.get("acquired_at"),
            **{key: value for key, value in spec.items() if key != "id"})
        _require(_same(recorded, capture) and _same(row.get("request"), capture["request"])
            and row.get("request_sha256") == capture["request_sha256"]
            and row.get("raw_sha256") == capture["wire_sha256"]
            and type(row.get("raw_bytes")) is int and row["raw_bytes"] == capture["wire_bytes"],
            "CAPTURE_CHANGED")
        transport = row.get("transport")
        _require(isinstance(transport, dict) and type(transport.get("http_status")) is int
            and transport["http_status"] == 200 and transport.get("wire_sha256") == capture["wire_sha256"]
            and type(transport.get("wire_bytes")) is int and transport["wire_bytes"] == capture["wire_bytes"],
            "TRANSPORT_CHANGED")
        _require(started <= _time(row.get("requested_at")) <= _time(capture["acquired_at"]) <= completed,
                 "CLOCK")
        view = capture["normalized"]
        venue = "SSE" if spec["symbol"].endswith(".SH") else "SZSE"
        bars = view["series"][spec["symbol"]]
        _require(view["counts"]["quarantined_rows"] == 0 and _same(row.get("counts"), view["counts"])
            and len(bars) >= 61 and [bar["trade_date"] for bar in bars] == verification["sessions"][venue],
            "HISTORY_INCOMPLETE")
        if spec["adjust_kind"] == "none":
            _require(row.get("close_matches_baseline") is True
                and _price(bars[-1]["close"]) == _price(verification["close"][spec["symbol"]]), "CLOSE_MISMATCH")
        captures[role] = capture
    _require(report["results"][1].get("repeat_equal") is True
        and captures[ROLES[0]]["normalized"]["series"] == captures[ROLES[1]]["normalized"]["series"]
        and captures[ROLES[0]]["wire_sha256"] == captures[ROLES[1]]["wire_sha256"], "REPEAT_CHANGED")
    return {"proof": proof, "archive": archive, "documents": docs, "captures": captures}


def source_for_kernel_evidence(archive, proof):
    checked = read_kernel_archive(archive, proof)
    content = {"schema": EVIDENCE_SCHEMA, "proof": checked["proof"], "archive_encoding": "base64",
               "archive_base64": base64.b64encode(archive).decode("ascii")}
    return SourceInput(source_id="ftshare-kernel-evidence-" + checked["proof"]["sha256"][:32],
        kind="content", title="FTShare Kernel v1 original four-request study artifact",
        origin="https://github.com/" + REPOSITORY + "/actions/runs/" + str(checked["proof"]["run_id"]),
        text=encoded(content).decode(), custody="provided",
        acquired_at=checked["documents"]["result.json"]["completed_at"],
        limitations=("ORIGINAL_ACTIONS_ZIP_BASE64_WITH_NATIVE_HOST_PROOF_NOT_A_PROVIDER_BODY",
            "FOUR_HISTORICAL_SOURCE_REQUESTS_SIX_CUMULATIVE_NOT_NEW_ADOPTION_HTTP",
            "TWO_RAW_SECURITIES_ONE_FORWARD_SECURITY_NOT_FULL_MARKET_QUALIFICATION",
            "PRIVATE_SOURCE_CUSTODY_NOT_PUBLIC_REDISTRIBUTION"))


def restore_kernel_evidence(reference, store):
    """Rebuild durable archive evidence without contacting GitHub Actions."""
    with market_read_scope(store) as reader:
        ref = reference if isinstance(reference, SourceRef) else SourceRef.from_dict(reference)
        _require(ref.selection == Selection() and ref.manifest.repository == reader.backend.repository,
                 "EXACT_LOCAL_REFERENCE")
        _, stored = _read_source(reader, ref)
        value = _document(stored["data"].encode())
        _require(isinstance(value, dict) and set(value) == {
            "schema", "proof", "archive_encoding", "archive_base64"}
            and value["schema"] == EVIDENCE_SCHEMA and value["archive_encoding"] == "base64"
            and isinstance(value["archive_base64"], str)
            and len(value["archive_base64"]) <= (MAX_ARCHIVE_BYTES + 2) // 3 * 4, "EVIDENCE")
        try:
            archive = base64.b64decode(value["archive_base64"], validate=True)
        except ValueError:
            raise RecordError("FTSHARE_ADOPTION_ARCHIVE_ENCODING") from None
        checked = read_kernel_archive(archive, value["proof"])
        expected = source_for_kernel_evidence(archive, value["proof"])
        _require(_same({**stored["manifest"]["input"], "text": stored["data"]}, asdict(expected)),
                 "EVIDENCE_SOURCE_CHANGED")
        return checked
